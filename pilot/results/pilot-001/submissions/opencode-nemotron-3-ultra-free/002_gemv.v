module dut #(
    parameter int ROWS = 16,
    parameter int COLS = 64,
    parameter int LANES = 16,
    parameter int DATA_W = 8,
    parameter int ACC_W = 32
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

    localparam int X_BEATS = COLS / LANES;
    localparam int A_BEATS = ROWS * COLS / LANES;
    localparam int ROW_BITS = $clog2(ROWS);
    localparam int X_BEAT_BITS = $clog2(X_BEATS);
    localparam int A_BEAT_BITS = $clog2(A_BEATS);

    typedef enum logic [1:0] {
        IDLE    = 2'd0,
        LOAD_X  = 2'd1,
        COMPUTE = 2'd2,
        OUTPUT  = 2'd3
    } state_t;

    state_t state, next_state;

    reg signed [DATA_W-1:0] x_mem [0:COLS-1];
    reg signed [ACC_W-1:0] acc [0:ROWS-1];

    reg [X_BEAT_BITS:0] x_beat_cnt;
    reg [A_BEAT_BITS:0] a_beat_cnt;
    reg [ROW_BITS:0] out_cnt;

    wire x_fire = in_x_flat_valid & in_x_flat_ready;
    wire a_fire = in_a_flat_valid & in_a_flat_ready;
    wire o_fire = out_valid & out_ready;

    wire [ROW_BITS-1:0] curr_row = a_beat_cnt[A_BEAT_BITS-1:X_BEAT_BITS];
    wire [X_BEAT_BITS-1:0] curr_col_beat = a_beat_cnt[X_BEAT_BITS-1:0];

    reg signed [ACC_W-1:0] sum_products;
    always_comb begin
        sum_products = '0;
        for (int i = 0; i < LANES; i++) begin
            sum_products = sum_products + 
                $signed(in_a_flat[i*DATA_W +: DATA_W]) * 
                $signed(x_mem[curr_col_beat*LANES + i]);
        end
    end

    always_comb begin
        next_state = state;
        case (state)
            IDLE: begin
                if (in_x_flat_valid | in_a_flat_valid)
                    next_state = LOAD_X;
            end
            LOAD_X: begin
                if (x_fire & (x_beat_cnt == X_BEATS - 1))
                    next_state = COMPUTE;
            end
            COMPUTE: begin
                if (a_fire & (a_beat_cnt == A_BEATS - 1))
                    next_state = OUTPUT;
            end
            OUTPUT: begin
                if (o_fire & (out_cnt == ROWS - 1))
                    next_state = IDLE;
            end
        endcase
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= IDLE;
            x_beat_cnt <= '0;
            a_beat_cnt <= '0;
            out_cnt <= '0;
            for (int j = 0; j < COLS; j++) x_mem[j] <= '0;
            for (int j = 0; j < ROWS; j++) acc[j] <= '0;
        end else begin
            state <= next_state;

            case (state)
                IDLE: begin
                    if (next_state == LOAD_X) begin
                        x_beat_cnt <= '0;
                        a_beat_cnt <= '0;
                        out_cnt <= '0;
                        for (int j = 0; j < ROWS; j++) acc[j] <= '0;
                    end
                end
                
                LOAD_X: begin
                    if (x_fire) begin
                        for (int i = 0; i < LANES; i++) begin
                            x_mem[x_beat_cnt*LANES + i] <= in_x_flat[i*DATA_W +: DATA_W];
                        end
                        x_beat_cnt <= x_beat_cnt + 1;
                    end
                end
                
                COMPUTE: begin
                    if (a_fire) begin
                        acc[curr_row] <= acc[curr_row] + sum_products;
                        a_beat_cnt <= a_beat_cnt + 1;
                    end
                end
                
                OUTPUT: begin
                    if (o_fire) begin
                        out_cnt <= out_cnt + 1;
                    end
                end
            endcase
        end
    end

    assign in_x_flat_ready = (state == LOAD_X) & (x_beat_cnt < X_BEATS);
    assign in_a_flat_ready = (state == COMPUTE) & (a_beat_cnt < A_BEATS);
    assign out_valid = (state == OUTPUT) & (out_cnt < ROWS);
    assign out_c = acc[out_cnt];

endmodule