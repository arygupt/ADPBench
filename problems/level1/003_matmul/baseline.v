// Baseline: serial matrix multiply.
// Buffers both operands (row-major), then computes one element per K MACs and
// streams C row-major. Repeated transactions reset the capture state when the
// last output word is accepted.

module dut #(
    parameter M      = 8,
    parameter N      = 8,
    parameter K      = 16,
    parameter LANES  = 16,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,

    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,

    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,

    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam A_BEATS = (M * K) / LANES;
    localparam B_BEATS = (K * N) / LANES;

    localparam S_RECV = 2'd0, S_MAC = 2'd1, S_OUT = 2'd2;

    reg [1:0] state;
    integer   a_beat;
    integer   b_beat;
    integer   i;
    integer   j;
    integer   k;
    reg       a_done;
    reg       b_done;
    reg signed [ACC_W-1:0] acc;
    reg [DATA_W-1:0] a_mem [0:M*K-1];
    reg [DATA_W-1:0] b_mem [0:K*N-1];
    integer   idx;

    assign in_a_flat_ready = (state == S_RECV) && !a_done;
    assign in_b_flat_ready = (state == S_RECV) && !b_done;
    assign out_valid       = (state == S_OUT);
    assign out_c           = acc;

    wire signed [DATA_W-1:0]   a_el = a_mem[i*K + k];
    wire signed [DATA_W-1:0]   b_el = b_mem[k*N + j];
    wire signed [2*DATA_W-1:0] prod = a_el * b_el;

    always @(posedge clk) begin
        if (!rst_n) begin
            state  <= S_RECV;
            a_beat <= 0;
            b_beat <= 0;
            i      <= 0;
            j      <= 0;
            k      <= 0;
            a_done <= 1'b0;
            b_done <= 1'b0;
            acc    <= {ACC_W{1'b0}};
        end else begin
            case (state)
                S_RECV: begin
                    if (!a_done && in_a_flat_valid) begin
                        for (idx = 0; idx < LANES; idx = idx + 1)
                            a_mem[a_beat*LANES + idx] <= in_a_flat[idx*DATA_W +: DATA_W];
                        if (a_beat == A_BEATS - 1) begin
                            a_beat <= 0;
                            a_done <= 1'b1;
                        end else begin
                            a_beat <= a_beat + 1;
                        end
                    end
                    if (!b_done && in_b_flat_valid) begin
                        for (idx = 0; idx < LANES; idx = idx + 1)
                            b_mem[b_beat*LANES + idx] <= in_b_flat[idx*DATA_W +: DATA_W];
                        if (b_beat == B_BEATS - 1) begin
                            b_beat <= 0;
                            b_done <= 1'b1;
                        end else begin
                            b_beat <= b_beat + 1;
                        end
                    end
                    if (a_done && b_done) begin
                        i   <= 0;
                        j   <= 0;
                        k   <= 0;
                        acc <= {ACC_W{1'b0}};
                        state <= S_MAC;
                    end
                end

                S_MAC: begin
                    acc <= acc + prod;
                    if (k == K - 1) begin
                        k     <= 0;
                        state <= S_OUT;
                    end else begin
                        k <= k + 1;
                    end
                end

                S_OUT: begin
                    if (out_ready) begin
                        acc <= {ACC_W{1'b0}};
                        if (j == N - 1) begin
                            j <= 0;
                            if (i == M - 1) begin
                                i      <= 0;
                                a_done <= 1'b0;
                                b_done <= 1'b0;
                                state  <= S_RECV;
                            end else begin
                                i     <= i + 1;
                                state <= S_MAC;
                            end
                        end else begin
                            j     <= j + 1;
                            state <= S_MAC;
                        end
                    end
                end

                default: state <= S_RECV;
            endcase
        end
    end

endmodule
