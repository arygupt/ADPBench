// Baseline: serial matrix-vector product.
// Buffers x, then streams A one beat at a time, MACing one element per cycle.
// One output word per row. Repeated transactions clear x_beat/row state when
// the last row's output is accepted.

module dut #(
    parameter ROWS   = 16,
    parameter COLS   = 64,
    parameter LANES  = 16,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,

    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,

    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,

    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam X_BEATS   = COLS / LANES;
    localparam ROW_BEATS = COLS / LANES;

    localparam S_RECV = 2'd0, S_A = 2'd1, S_MAC = 2'd2, S_OUT = 2'd3;

    reg [1:0]  state;
    integer    x_beat;
    integer    col_beat;
    integer    row;
    integer    lane;
    reg signed [ACC_W-1:0]      acc;
    reg [DATA_W-1:0]            x_mem [0:COLS-1];
    reg [LANES*DATA_W-1:0]      a_hold;
    integer    k;

    assign in_x_flat_ready = (state == S_RECV);
    assign in_a_flat_ready = (state == S_A);
    assign out_valid       = (state == S_OUT);
    assign out_c           = acc;

    wire signed [DATA_W-1:0]   a_el = a_hold[lane*DATA_W +: DATA_W];
    wire signed [DATA_W-1:0]   x_el = x_mem[col_beat*LANES + lane];
    wire signed [2*DATA_W-1:0] prod = a_el * x_el;

    always @(posedge clk) begin
        if (!rst_n) begin
            state    <= S_RECV;
            x_beat   <= 0;
            col_beat <= 0;
            row      <= 0;
            lane     <= 0;
            acc      <= {ACC_W{1'b0}};
        end else begin
            case (state)
                S_RECV: begin
                    if (in_x_flat_valid) begin
                        for (k = 0; k < LANES; k = k + 1)
                            x_mem[x_beat*LANES + k] <= in_x_flat[k*DATA_W +: DATA_W];
                        if (x_beat == X_BEATS - 1) begin
                            x_beat <= 0;
                            state  <= S_A;
                        end else begin
                            x_beat <= x_beat + 1;
                        end
                    end
                end

                S_A: begin
                    if (in_a_flat_valid) begin
                        a_hold <= in_a_flat;
                        lane   <= 0;
                        state  <= S_MAC;
                    end
                end

                S_MAC: begin
                    acc <= acc + prod;
                    if (lane == LANES - 1) begin
                        lane <= 0;
                        if (col_beat == ROW_BEATS - 1) begin
                            col_beat <= 0;
                            state    <= S_OUT;
                        end else begin
                            col_beat <= col_beat + 1;
                            state    <= S_A;
                        end
                    end else begin
                        lane <= lane + 1;
                    end
                end

                S_OUT: begin
                    if (out_ready) begin
                        acc <= {ACC_W{1'b0}};
                        if (row == ROWS - 1) begin
                            row      <= 0;
                            col_beat <= 0;
                            state    <= S_RECV;
                        end else begin
                            row   <= row + 1;
                            state <= S_A;
                        end
                    end
                end

                default: state <= S_RECV;
            endcase
        end
    end

endmodule
