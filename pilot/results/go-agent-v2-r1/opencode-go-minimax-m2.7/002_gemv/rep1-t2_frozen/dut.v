// ADPBench submission. Replace the body; keep the module name and ports.
// Call the native check() tool for synthesis + simulation feedback.

module dut #(
    parameter ROWS = 16,
    parameter COLS = 64,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    output reg                     out_valid,
    input  wire                    out_ready,
    output reg signed [ACC_W-1:0]  out_c
);

    // Constants
    localparam A_ELEMENTS = ROWS * COLS;  // 1024
    localparam X_ELEMENTS = COLS;          // 64
    localparam A_BEATS = A_ELEMENTS / LANES;  // 64
    localparam X_BEATS = X_ELEMENTS / LANES;  // 4

    // RAM for matrix A
    reg [DATA_W-1:0] a_mem [0:A_ELEMENTS-1];
    // RAM for vector x
    reg [DATA_W-1:0] x_mem [0:X_ELEMENTS-1];

    // State machine states
    localparam S_IDLE = 2'd0;
    localparam S_RECV = 2'd1;
    localparam S_COMPUTE = 2'd2;
    localparam S_OUTPUT = 2'd3;

    // Registers
    reg [1:0] state;
    reg [9:0] a_waddr;
    reg [5:0] x_waddr;
    reg [6:0] a_rcvd;
    reg [2:0] x_rcvd;
    reg a_done, x_done;
    reg [3:0] row;
    reg [5:0] col;
    reg [ACC_W-1:0] accumulator;
    reg computing_done;
    reg [3:0] out_idx;
    reg [ACC_W-1:0] row_result [0:ROWS-1];

    // Accept signals
    wire a_accept = in_a_flat_ready && in_a_flat_valid;
    wire x_accept = in_x_flat_ready && in_x_flat_valid;

    // Ready generation
    assign in_a_flat_ready = (state == S_IDLE) || (state == S_RECV && !a_done);
    assign in_x_flat_ready = (state == S_IDLE) || (state == S_RECV && !x_done);

    // State and data path
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE;
            a_waddr <= 10'd0;
            x_waddr <= 6'd0;
            a_rcvd <= 7'd0;
            x_rcvd <= 3'd0;
            a_done <= 1'b0;
            x_done <= 1'b0;
            out_valid <= 1'b0;
            out_idx <= 4'd0;
            row <= 4'd0;
            col <= 6'd0;
            accumulator <= 32'd0;
            computing_done <= 1'b0;
        end else begin
            case (state)
                S_IDLE: begin
                    a_waddr <= 10'd0;
                    x_waddr <= 6'd0;
                    a_rcvd <= 7'd0;
                    x_rcvd <= 3'd0;
                    a_done <= 1'b0;
                    x_done <= 1'b0;
                    out_valid <= 1'b0;
                    out_idx <= 4'd0;
                    computing_done <= 1'b0;
                    if (a_accept || x_accept) begin
                        state <= S_RECV;
                    end
                end

                S_RECV: begin
                    if (a_accept) begin
                        integer i;
                        for (i = 0; i < LANES; i = i + 1) begin
                            a_mem[a_waddr + i] <= in_a_flat[i*DATA_W +: DATA_W];
                        end
                        a_waddr <= a_waddr + LANES;
                        a_rcvd <= a_rcvd + 1'b1;
                        if (a_rcvd == A_BEATS - 1) a_done <= 1'b1;
                    end
                    if (x_accept) begin
                        integer i;
                        for (i = 0; i < LANES; i = i + 1) begin
                            x_mem[x_waddr + i] <= in_x_flat[i*DATA_W +: DATA_W];
                        end
                        x_waddr <= x_waddr + LANES;
                        x_rcvd <= x_rcvd + 1'b1;
                        if (x_rcvd == X_BEATS - 1) x_done <= 1'b1;
                    end
                    if (a_done && x_done) begin
                        state <= S_COMPUTE;
                        row <= 4'd0;
                        col <= 6'd0;
                        accumulator <= 32'd0;
                    end
                end

                S_COMPUTE: begin
                    if (!computing_done) begin
                        // Get current elements
                        reg signed [DATA_W-1:0] a_val;
                        reg signed [DATA_W-1:0] x_val;
                        reg signed [ACC_W-1:0] prod;
                        reg [ACC_W-1:0] new_acc;

                        a_val = a_mem[row * COLS + col];
                        x_val = x_mem[col];
                        prod = $signed({{(ACC_W-DATA_W){a_val[DATA_W-1]}}, a_val}) *
                               $signed({{(ACC_W-DATA_W){x_val[DATA_W-1]}}, x_val});
                        new_acc = accumulator + prod;

                        accumulator <= new_acc;
                        col <= col + 1'b1;

                        if (col == COLS - 1) begin
                            // End of row - save result
                            row_result[row] <= new_acc;
                            col <= 6'd0;
                            accumulator <= 32'd0;

                            if (row == ROWS - 1) begin
                                // All rows complete
                                computing_done <= 1'b1;
                                out_idx <= 4'd0;
                                out_valid <= 1'b1;
                                out_c <= new_acc;
                                state <= S_OUTPUT;
                            end else begin
                                row <= row + 1'b1;
                            end
                        end
                    end
                end

                S_OUTPUT: begin
                    if (out_ready && out_valid) begin
                        if (out_idx < ROWS - 1) begin
                            out_idx <= out_idx + 1'b1;
                            out_c <= row_result[out_idx + 1'b1];
                        end else begin
                            out_valid <= 1'b0;
                            state <= S_IDLE;
                        end
                    end
                end
            endcase
        end
    end

endmodule
